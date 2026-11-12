I'll rewrite the article to drop the tics in your screenshot, then put it in an editable doc. Loading the docs guide first.

Here's the rewrite. I kept every figure and file reference from the original and checked the draft against the ten tics in your screenshot.

---

# Why we stopped trying to beat ripgrep for coding agents

For a few months we were sure coding agents needed a better way to search code.

Watching an LLM work in a repository with `ripgrep` (`rg`) and file reads looks clumsy at first. It greps for a function name, gets forty hits, opens three files it didn't need, and eventually lands on the right call site. As systems engineers, our instinct was to give the model a structural index. We built custom tools (`mip_open`, `mip_impact`, `mip_refs`) so it could query call graphs, symbol definitions, and impact trees directly.

Then we ran controlled, head-to-head benchmarks against plain `rg` plus `read_file`, and the results humbled us. On routine single-file edits, a frontier model with nothing but `rg` and `read_file` was already fast, cheap, and accurate. The custom tools made things worse. Once an agent had extra navigation tools, it used them, or spent tokens deciding whether to, even on a three-line bugfix. We called this the **local-edit tax**. We were adding friction to ordinary edits in exchange for help on cross-file contract changes.

At that point we had two options: keep tuning tool descriptions and hope to beat `rg` at search, or read the failure transcripts and work out what was going wrong. We read the transcripts.

## The failure we kept seeing: premature completion

We went through the transcripts where `gpt-6.1-sol`, running in Codex, failed our multi-file tasks. The model usually managed to use `rg`. When it knew a caller existed, it found the caller and fixed it. Where it went wrong was stopping too early.

One example from the real 22-file source tree of `pypa/packaging`, the third-party Python library that parses `pip` requirements, shows the pattern. We asked the agent to update `parse_requirement` in `packaging/_parser.py` so that whitespace-only strings raise a dedicated `EmptyRequirementError` instead of a generic syntax error.

In our control run (Arm A, using standard `rg`, `read`, and `write`), the agent did three things. It read `packaging/errors.py` and added `class EmptyRequirementError(Exception)`. It read `packaging/_parser.py` and raised the new error when the input was empty. Then it declared the task done.

The run took 41.39 seconds, and the code looked clean. It was also broken at the public caller. Two files over, `packaging/requirements.py:102` wraps `_parse_requirement(requirement_string)` in a narrow `try ... except ParserSyntaxError:` block so it can re-raise a user-facing `InvalidRequirement`. `EmptyRequirementError` didn't inherit from `ParserSyntaxError`, and nobody added it to that `except` clause. So `Requirement("   ")` let an internal exception escape through the public API.

The local file looked finished, so the agent never ran the search that would have found the caller. `rg` could have found it without trouble.

Across our contract-change tasks, we saw four recurring kinds of silent drift:

1. **Return and exception drift.** A function starts returning a 3-tuple where callers unpack two values, so `a, b = verify_token()` crashes. Or it raises a new exception that slips past a caller's narrow `try/except`.
2. **Wrapper and subclass drift.** Someone adds `base_iri=None` to `Graph.parse()`, but `ConjunctiveGraph.parse()` overrides or wraps it without forwarding the argument.
3. **Callback and descriptor tables.** A required keyword-only argument (`*, strict: bool`) goes onto `dump_header` in `werkzeug/http.py`. Direct calls get updated, but `header_property(..., dump_func=dump_header)` in `werkzeug/sansio/response.py` passes `dump_header` as a bare reference, so that binding goes stale.
4. **Dynamic dispatch and chained attributes.** A change to `NTSerializer.serialize` or `OptionValidator.validate` breaks callers that reach it through a plugin registry (`plugin.serialize(...)`, `v.validate(...)`) or a multi-hop chain like `self.session.token_store.issue(...)`.

The test suite misled the agent, too. In the dynamic plugin tasks, the agent ran the configured unit tests after changing `NTSerializer.serialize`. The existing tests called `exporter.serialize("nt", triples)` with the old default arguments, so the command exited with `exit_code=0`. Green tests convinced the agent it was finished. Passing the new `base_iri` argument through `GraphExporter.serialize` would have crashed with a `TypeError`.

## What we changed: remove the search tools, watch the write and test steps

Once we framed the problem as premature completion after a contract-changing edit, the design changed in three ways.

First, we removed the custom `mip_*` search tools from the agent's prompt. It uses `rg`, `read`, `write`, and `test` as usual.

Second, we attached a delta detector, `mipdelta`, to the `write` and `test` responses. Each time the agent writes a file, `mipdelta` compares the file's AST before and after the edit against a warm in-memory symbol index.

Third, it stays quiet unless something matters. If the edit doesn't change a tracked contract, the output gains zero bytes. If the edit opens a candidate cross-file seam, `mipdelta` appends one advisory line of at most 250 UTF-8 bytes. The line names the affected consumer's file and line, the detector, a provenance tag (`[static]`, `[rt-obs]`, or `[type-cand]`), and the specific mismatch when there's room.

For the packaging failure, the `write` response came back with 197 extra bytes at the bottom:

```text
[mip-delta] packaging/requirements.py:102 exception_contract_review[static] parse_requirement (parse_requirement newly raises EmptyRequirementError, bypassing caller try/except (ParserSyntaxError))
```

The advisory points at one specific failure: the new exception gets past this caller's handler. That gives the agent a concrete reason to keep working before it finalizes.

## Going beyond static analysis

Pure AST diffing catches direct calls, subclass overrides, and callback tables like `header_property(..., dump_header)`. It stays silent when a callback is already wrapped in a `lambda` or `functools.partial`, and it can't always resolve the receiver of a method call. We added two more sources of evidence.

**Runtime observation (`[rt-obs]`).** Python 3.12 added [PEP 669](https://peps.python.org/pep-0669/), `sys.monitoring`, a hook into VM events. We wrote a small `sitecustomize` collector, `mipdelta.monitoring`, that switches on when the agent runs tests. When a repo function calls a method on an object, the collector logs an edge: the caller's file and line, the receiver's runtime type, and the method name. Each call site is capped to keep overhead down, which trades completeness for speed.

Agents often edit the target file first and run tests second, so we made this order-independent. When a test command finishes, `mipdelta` reads the runtime edges from that run and re-checks any files the agent already changed in the session.

In the `rdflib_pkg` plugin task (OSS05), the agent added `*, base_iri=None` to `NTSerializer.serialize` and ran the unit tests. They passed with `exit_code=0` because they only exercised the default call, `exporter.serialize("nt", triples)`. The control arm saw green and finished.

The `mipdelta` arm got the same passing test run, but `sys.monitoring` had recorded `GraphExporter.serialize` in `rdflib_pkg/exporter.py:7` calling `NTSerializer.serialize` during that run. Below the passing output, `mipdelta` added:

```text
[mip-delta] rdflib_pkg/exporter.py:7 delegating_wrapper_review[rt-obs] GraphExporter.serialize (delegating wrapper GraphExporter.serialize calls NTSerializer.serialize without forwarding optional parameter base_iri)
```

The agent opened `rdflib_pkg/exporter.py`, forwarded `base_iri`, and passed the post-episode oracle.

**Stub-typed chains (`[type-cand]`).** When a workspace includes `.pyi` stubs or inline attribute annotations, `mipdelta` reads them and folds attribute and return types into its symbol resolver. We tested this on a 109-file `docutils` workspace with benchmark-supplied `nodes.pyi` and `transforms/__init__.pyi` stubs (OSS07), and on a 9-file `web_pkg` fixture with `context.pyi` (OSS08). When the agent modified `Reporter.attach_observer` or `TokenStore.issue`, `mipdelta` followed the typed chains `self.document.reporter.attach_observer` and `self.session.token_store.issue`. It flagged `docutils/parsers/__init__.py:87`, `docutils/transforms/__init__.py:183`, and `web_pkg/views.py:5`.

## How we measured it

**Local commits.** We replayed 50 real commits from `click`, `packaging`, `platformdirs`, `tomlkit`, and `werkzeug`. A mechanical filter picked them, and each changed only the body of one function, with no signature or symbol changes. `mipdelta` stayed silent on all 50 (0 of 50 commits). A warm incremental `apply_write` took a median of 5.73 ms (p95 28.71 ms). The CPU median was 6.64 ms (p95 32.81 ms), excluding cold workspace indexing.

**Historical replays.** On 10 historical miss cases, the revised detector cut candidate warnings from 53 to 10, an 81.13% reduction in chaff, at an 8.32 ms warm median.

**Live paired runs.** We ran two paired evaluations with `codex-cli` (`gpt-6.1-sol`). Arm A had `rg`, `read`, `write`, and `test`. Arm B had the same tools plus the 250-byte advisory on `write` and `test` responses. That's 14 tasks and 28 episodes. Six were small constructed pilots (L01–L06), and eight used real OSS trees or stubbed workspaces.

| Task group | Tasks (episodes) | Arm A passed | Arm B passed | Arm B advisory bytes | Flagged files later edited (Arm B) |
| :--- | ---: | ---: | ---: | ---: | ---: |
| Constructed local-edit pilot (L01–L02) | 2 (4) | 2/2 | 2/2 | 0 | 0/0 (silent) |
| Real-OSS local-edit commits (OSS01 `platformdirs`, OSS02 `werkzeug`) | 2 (4) | 2/2 | 2/2 | 0 | 0/0 (silent) |
| Constructed static contract pilot (L03–L06) | 4 (8) | 2/4 | 4/4 | 89–213 (3 tasks) | 3/3 triggered (L04 pre-satisfied) |
| Real-OSS static contract (OSS03 `werkzeug`, 52 files; OSS04 `packaging`, 22 files) | 2 (4) | 1/2 | 2/2 | 197–241 | 2/2 triggered |
| Constructed runtime `sys.monitoring` (OSS05 `rdflib_pkg`, OSS06 `cli_pkg`) | 2 (4) | 0/2 | 2/2 | 197–430 | 2/2 triggered |
| Stub-typed (OSS07 `docutils`, 109 files plus added `.pyi`; OSS08 `web_pkg`) | 2 (4) | 1/2 | 2/2 | 158–202 | 2/2 triggered |
| **Total** | **14 (28)** | **8/14** | **14/14** | **0 on local edits** | **9/9 triggered tasks** |

Arm B passed 14 of 14 tasks. Arm A passed 8. Arm B's advisory added nothing on local edits. In all nine tasks where `mipdelta` triggered, the agent went on to edit the flagged file.

The advisory column totals the advisory text across each episode, so it is not a per-response size. In OSS05, the 430 bytes were two 215-byte advisories, one on the `test` response at sequence 3 and one at sequence 10. OSS06 received one 197-byte advisory on its sequence 4 `test` response. Each advisory stayed under the 250-byte cap. These figures leave out JSON framing, and the gateway separately limits added wire bytes to 350.

Two large OSS trees passed in both arms, so we can compare their token use. Episodes ran A and then B, in that order.

- **werkzeug (52 files, OSS03).** Arm A used 17 calls, 94.83 s wall time, 352,660 input tokens (289,280 cached), and 2,072 output tokens. Arm B used 14 calls, 75.64 s, 255,529 input tokens (225,408 cached), and 2,366 output tokens.
- **docutils (109 files plus `.pyi`, OSS07).** Arm A used 12 calls, 55.52 s, 215,772 input tokens (159,616 cached), and 1,240 output tokens. Arm B used 11 calls, 42.67 s, 191,938 input tokens (162,944 cached), and 1,092 output tokens.

Arm B made fewer calls and consumed fewer input tokens in both runs. Non-cached tokens (input minus cached input, plus output) were 32,487 for Arm B versus 65,452 for Arm A on werkzeug, and 30,086 versus 57,396 on docutils. That number moves with provider cache hits, so it doesn't measure work cleanly. Running A then B in sequence also can't separate our change from caching, disk warmth, service variance, or model variation. These numbers describe the runs. They don't establish a speedup. A proper timing comparison needs randomized arm order, repeated runs, and recorded cold and warm conditions.

## Testing the engine against a plain reminder

A skeptical reviewer asked how much of Arm B's advantage comes from `mipdelta`'s cross-file engine, and how much comes from interrupting the agent after a contract change.

To find out, we built Arm R, a generic contract reminder. Arm R has no repository index, no `.pyi` stubs, and no `sys.monitoring`. When a write changes a function's parameters, return tuple arity, `None` return, raised exceptions, or state fields within the edited file, Arm R appends a 189-byte reminder:

```text
[reminder] Contract change detected in this file: before finalizing, check callers, delegating wrappers, subclass overrides, return unpackers, and try/except handlers across the repository.
```

We ran Arm R on the six discordant tasks: L05, L06, OSS04, OSS05, OSS06, and OSS08. Arm A had failed all six, and Arm B had passed all six. Because we picked these tasks from earlier outcomes, treat this as a diagnostic check. It doesn't estimate how often either arm succeeds in general. Only the six R runs are new. The A and B results come from earlier runs.

| Task | Stratum | Arm A (calls, bytes) | Arm R (calls, bytes) | Arm B (calls, bytes) |
| :--- | :--- | :--- | :--- | :--- |
| L05 delegating wrapper forwarding | 2 (static) | FAIL (3, 1,172) | PASS (9, 4,797) | PASS (8, 3,519) |
| L06 return and exception contract | 2 (static) | FAIL (4, 1,413) | PASS (11, 6,827) | PASS (7, 2,972) |
| OSS04 packaging `parse_requirement` exception | 2 (OSS static) | FAIL (7, 27,061) | PASS (12, 41,458) | PASS (12, 41,473) |
| OSS05 rdflib runtime plugin serializer | 3 (rt-obs) | FAIL (4, 1,256) | FAIL (8, 5,045) | PASS (11, 5,555) |
| OSS06 click runtime command loader | 3 (rt-obs) | FAIL (5, 1,952) | PASS (11, 6,331) | PASS (8, 3,509) |
| OSS08 web stub session chain | 4 (type-cand) | FAIL (3, 746) | PASS (7, 3,032) | PASS (6, 2,005) |
| **Totals** | **Strata 2–4** | **0/6 (0.0%)** | **5/6 (83.3%)** | **6/6 (100.0%)** |

The generic reminder recovered five of the six historical failures. Across those five tasks, where R and B both passed, R made 50 calls and returned 62,445 bytes. B made 41 calls and returned 53,478 bytes. R used 22.0% more calls. B used 18.0% fewer calls and 14.4% fewer returned bytes. Pooled non-cached tokens were 148,779 for R and 153,496 for B, so fewer calls and bytes didn't translate into lower token cost. R also includes a local AST change detector. The result shows the value of a well-timed contract-review prompt. It doesn't show how much repository analysis adds on top.

The one B-only pass, OSS05, is harder to read. The reminder agent searched for `serialize|NTSerializer`, read `rdflib_pkg/exporter.py` and the registry, and got another reminder after a passing test. Then it finished without changing the wrapper. B received the specific forwarding warning and passed the oracle.

The ticket asks for the optional parameter on `NTSerializer.serialize`. The oracle also requires `GraphExporter.serialize` to expose it, even though the wrapper's current default behavior still works. Before we count this as a prevented defect, an independent review has to confirm that passing the option through the public wrapper was a stated requirement. We're keeping the recorded outcomes as they are. Future tickets will state the required public behavior directly and won't name the files to edit.

The next question is how R and B compare on larger repositories. The current fixtures are small: 7 source files each in L05 and L06, 9 in OSS06, 9 in OSS08 (8 `.py` plus one `.pyi`), and 22 in the packaging task. With search spaces this small, we can't say much about navigation cost, and we didn't measure homonym noise. The existing 52-file werkzeug and 109-file docutils results compare A with B, not R with B. They suggest that specific `file:line` evidence might save more searching and reading as lexical ambiguity and cross-module obligations grow. They don't show that a generic reminder forces broad grep sweeps on those repos.

Testing that hypothesis needs fresh tasks with independently written specs, randomized R/B order, repeated episodes, and measurements of irrelevant search hits, navigation calls, accepted patches, tokens, and wall time. Include cases where a wrapper should deliberately not expose a new option. File count by itself doesn't measure search difficulty.

## What we kept from Semantic LOD

We stopped asking the agent to navigate a separate structural tool catalog. The source anchors, caller graph, and selection logic still seem useful, so we kept them as research components.

One experiment compiles a task brief before the agent starts, then gives the agent its usual search, read, edit, and test tools. The brief has a fixed size budget and labels the exact source lines it includes. Separately, we're exploring executable architecture rules that would check a patch in CI.

The one-shot pilot compares three starting points: the ticket alone, a simple symbol map with lexical excerpts, and a Semantic LOD brief with related source spans. Its offline preflight checks delivery and fixture behavior. In the live pilot, every arm passed all three constructed tasks. Semantic LOD returned fewer tool bytes. The no-brief arm used the fewest total provider tokens and had the lowest wall time. The pilot ran one episode per task and arm, which is too thin to rank them. We're keeping the compiler as an experiment alongside the advisory CLI.

## Open questions

These results show that in selected Python contract-change tasks, short source-anchored warnings after edits or test runs helped agents fix omissions that search and passing baseline tests missed. Several questions remain open.

- **Reminder versus engine at scale.** The head-to-head on larger repositories hasn't been run. Until it is, we shouldn't infer search difficulty from file count, and we shouldn't pick tasks because they showed a B win.
- **Intentional breaking changes.** Our 50-commit corpus confirmed that `mipdelta` stays quiet when signatures and symbols don't change. It doesn't measure how much noise a developer gets when they deliberately change an API, or when they write a wrapper that hides an inner parameter on purpose.
- **Sampling versus completeness.** To keep `sys.monitoring` overhead low during `pytest`, the collector caps observations at `max_events_per_site=8` per call site. In our offline Click benchmark, that cap found 88 polymorphic sites where unbounded monitoring found 206. Runtime edges are positive evidence of paths that ran. They don't prove that unobserved callers don't exist.
- **Real-world use.** Constructed and maintainer-curated benchmarks can validate a mechanism. Validating the product takes independent Python developers running `mip-delta --check-git` and `mip-delta --run-pytest` on their everyday work, and choosing to keep it enabled.
