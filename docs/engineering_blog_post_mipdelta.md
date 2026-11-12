# Why We Stopped Trying to Beat ripgrep for Coding Agents

For a few months, we were sure coding agents needed a better way to search code.

Watching an LLM work inside a repository with `ripgrep` (`rg`) and file reads looks clumsy at first. It greps for a function name, gets forty hits, opens three files it didn't need, and eventually lands on the right call site. As systems engineers, our instinct was to give the model a structural index. We built custom tools (`mip_open`, `mip_impact`, `mip_refs`) so it could query call graphs, symbol definitions, and impact trees directly.

Then we ran controlled, head-to-head benchmarks against plain `rg` plus `read_file`, and the results humbled us. 

On routine single-file edits, a frontier model with nothing but `rg` and `read_file` was already fast, cheap, and accurate. The custom tools made things worse. Once an agent had extra navigation tools, it used them, or spent tokens deciding whether to, even on a three-line bugfix. We called this the **local-edit tax**. We were adding friction to ordinary edits in exchange for help on cross-file contract changes.

At that point we had two options: keep tuning tool descriptions and hope to beat `rg` at search, or read the failure transcripts and work out what was actually going wrong. 

We read the transcripts.

---

## The Failure Mode We Kept Seeing: Premature Completion

When we went through the transcripts where `gpt-6.1-sol` (running in Codex) failed our multi-file tasks, the pattern was clear. The model rarely failed because it couldn't use `rg`. When it knew a caller existed, it found the caller and fixed it. 

Where it went wrong was **stopping too early**.

One example from the real 22-file source tree of `pypa/packaging` (the standard library that parses `pip` requirements) shows the dynamic clearly. We asked the agent to update `parse_requirement` in `packaging/_parser.py` so that whitespace-only strings raise a dedicated `EmptyRequirementError` instead of a generic syntax error.

In our control run (Arm A, using standard `rg`, `read`, and `write`), the agent did three things:
1. It read `packaging/errors.py` and added `class EmptyRequirementError(Exception)`.
2. It read `packaging/_parser.py` and raised the new error when the input was empty.
3. It declared the task done and finalized.

The run took 41.39 seconds, and the diff looked clean. It was also broken at the public caller. 

Two files over, `packaging/requirements.py:102` wraps `_parse_requirement(requirement_string)` in a narrow `try ... except ParserSyntaxError:` block so it can re-raise a user-facing `InvalidRequirement`. `EmptyRequirementError` didn't inherit from `ParserSyntaxError`, and nobody had added it to that `except` clause. Calling `Requirement("   ")` let an internal exception escape straight through the public API.

The agent didn't fail because `rg` couldn't find `_parse_requirement`. It failed because the local edit looked complete, so **it never ran the search**.

Across our contract-change tasks, we saw four recurring kinds of silent drift:
1. **Return and exception drift.** A function starts returning a 3-tuple where callers unpack two values (`a, b = verify_token()`), or raises a new exception that slips past a caller's narrow `try/except`.
2. **Wrapper and subclass drift.** Someone adds `base_iri=None` to `Graph.parse()`, but `ConjunctiveGraph.parse()` overrides or wraps it without forwarding the argument.
3. **Callback and descriptor tables.** A required keyword-only argument (`*, strict: bool`) goes onto `dump_header` in `werkzeug/http.py`. Direct calls get updated, but `header_property(..., dump_func=dump_header)` in `werkzeug/sansio/response.py` passes `dump_header` as a bare reference, so that binding goes stale.
4. **Dynamic dispatch and chained attributes.** A change to `NTSerializer.serialize` or `OptionValidator.validate` breaks callers that reach it through a plugin registry (`plugin.serialize(...)`, `v.validate(...)`) or a multi-hop chain like `self.session.token_store.issue(...)`.

The test suite misled the agent, too. In the dynamic plugin tasks, the agent ran the configured unit tests after changing `NTSerializer.serialize`. The existing tests called `exporter.serialize("nt", triples)` with the old default arguments, so the command exited with `exit_code=0`. Green tests convinced the agent it was finished. Passing the new `base_iri` argument through `GraphExporter.serialize` would have crashed with a `TypeError`.

---

## What We Changed: Remove the Search Tools, Watch the Write and Test Steps

Once we framed the problem as premature completion after a contract-changing edit, the architecture flipped upside down:

1. We removed the custom `mip_*` search tools from the agent's prompt entirely. It uses `rg`, `read`, `write`, and `test` as usual.
2. We attached a delta detector, `mipdelta`, to the `write` and `test` responses. Each time the agent writes a file, `mipdelta` compares the file's AST before and after the edit against a warm in-memory symbol index.
3. It stays quiet unless something matters. If the edit doesn't change a tracked contract, the output gains zero bytes.
4. If the edit opens a candidate cross-file seam, `mipdelta` appends one advisory line of at most 250 UTF-8 bytes. The line names the affected consumer's file and line, the detector, a provenance tag (`[static]`, `[rt-obs]`, or `[type-cand]`), and the specific mismatch evidence when there is room.

For the packaging failure, the `write` response came back with 197 extra bytes at the bottom:

```text
[mip-delta] packaging/requirements.py:102 exception_contract_review[static] parse_requirement (parse_requirement newly raises EmptyRequirementError, bypassing caller try/except (ParserSyntaxError))
```

The advisory points at one specific failure: the new exception gets past this caller's handler. That gives the agent a concrete reason to keep working before it finalizes.

---

## Going Beyond Pure Static Analysis

Pure AST diffing catches direct calls, subclass overrides, and callback tables like `header_property(..., dump_header)`. It stays silent when a callback is already wrapped in a `lambda` or `functools.partial`, and it can't always resolve the receiver of a method call. We added two more sources of evidence.

**Runtime observation (`[rt-obs]`).** Python 3.12 added [PEP 669](https://peps.python.org/pep-0669/), `sys.monitoring`, an efficient hook into VM events. We wrote a small `sitecustomize` collector, `mipdelta.monitoring`, that switches on when the agent runs tests. When a repo function calls a method on an object, the collector logs an edge: the caller's file and line, the receiver's runtime type, and the method name. Each call site is capped to keep overhead down, trading completeness for speed.

When a test command finishes, `mipdelta` reads the runtime edges from that run and re-checks any files the agent already changed in the session.

In the `rdflib_pkg` plugin task (OSS05), the agent added `*, base_iri=None` to `NTSerializer.serialize` and ran the unit tests. They passed with `exit_code=0` because they only exercised the default call. The control arm saw green and finished. The `mipdelta` arm got the same passing test run, but `sys.monitoring` had recorded `GraphExporter.serialize` in `rdflib_pkg/exporter.py:7` calling `NTSerializer.serialize` during that run. Below the passing test output, `mipdelta` added:

```text
[mip-delta] rdflib_pkg/exporter.py:7 delegating_wrapper_review[rt-obs] GraphExporter.serialize (delegating wrapper GraphExporter.serialize calls NTSerializer.serialize without forwarding optional parameter base_iri)
```

The agent opened `rdflib_pkg/exporter.py`, forwarded `base_iri`, and passed the post-episode oracle.

**Stub-typed chains (`[type-cand]`).** When a workspace includes `.pyi` stubs or inline attribute annotations, `mipdelta` reads them and folds attribute and return types into its symbol resolver. We tested this on a 109-file `docutils` workspace with benchmark-supplied stubs (OSS07) and a 9-file `web_pkg` fixture (OSS08). When the agent modified `Reporter.attach_observer` or `TokenStore.issue`, `mipdelta` followed typed chains like `self.document.reporter.attach_observer` and flagged `docutils/parsers/__init__.py:87` and `web_pkg/views.py:5`.

---

## Early Synthetic and Codex Benchmarks

In paired evaluations with `codex-cli` (`gpt-6.1-sol`) across 14 tasks (28 episodes):
- Arm A (standard `rg`, `read`, `write`, `test`) passed **8/14** tasks.
- Arm B (same tools + 250-byte advisory) passed **14/14** tasks.
- On routine local edits, Arm B added **0 bytes** of overhead.
- In all 9 tasks where `mipdelta` triggered, the agent subsequently edited the flagged file.

To isolate whether the advantage came from repository analysis or simply interrupting the agent after a contract change, we built **Arm R**, a generic contract reminder that fires on any signature change without repository analysis:

```text
[reminder] Contract change detected in this file: before finalizing, check callers, delegating wrappers, subclass overrides, return unpackers, and try/except handlers across the repository.
```

On the six tasks Arm A had failed, Arm R recovered 5 of 6, but used 22.0% more calls and 14.4% more returned bytes than Arm B. The reminder proved that a well-timed interrupt has enormous value on its own—and set the stage for testing whether the engine generalized beyond our development fixtures.

---

## The Reality Check: 10 Unseen Repositories and 1,045 Commits

Constructed benchmarks and development fixtures will always overstate real-world performance. To test what `mipdelta` actually does on code nobody tuned it on, we froze the engine, wrote pre-registered evaluation plans, and replayed the tool over 800 non-merge commits across **10 mature, independently maintained open-source repositories**:
- **Round 1:** `attrs`, `httpx`, `marshmallow`, `rich`, `mkdocs`
- **Round 2:** `requests`, `urllib3`, `pytest`, `scrapy`, `paramiko`

Across all 10 repositories, there were **1,045 commits that altered a function's contract**.

### 1. It is quiet
Across the 10 repositories, the default check stayed silent on **1,008 of 1,045 contract-changing commits (96.5% silence)**. It does not spam the author on everyday refactorings.

### 2. When it reports, roughly half the flagged commits are actionable
Across those 1,045 commits, the tool flagged 37 commits. Independent model and human readers adjudicating each site found that **18 of the 37 flagged commits (48.6%) contained a real omission or a site well worth a reviewer's look**.

### 3. It caught real, unforced omissions in mature software
These were not synthetic tasks. These were commits that had already passed peer review and landed in production upstream repositories:
- **`paramiko` (`AgentKey.sign_ssh_data`):** An author added an `algorithm` parameter and never read it in the body, continuing to sign with SHA-1 when SHA-2 was requested (a subtle crypto/security bug, fixed upstream the very next commit).
- **`paramiko` (`SSHClient.connect` / `Transport.__init__`):** Accepted and documented `disable_algorithms` but forgot to forward it to the transport layer (fixed 2–3 commits later).
- **`scrapy` (`FeedExporter.item_scraped`):** A helper function was converted from synchronous to `async`. A caller stored the unstarted coroutine object instead of scheduling or awaiting it, silently dropping batch flushes during crawls (fixed upstream in #7730).
- **`httpx` (`AsyncClient._init_proxy_transport`):** The synchronous client forwarded a new `http1` parameter, while the async client accepted it and dropped it (fixed upstream in #3023).
- **`rich` (`Traceback.__init__`):** A caller passing options to `Traceback` had five locals parameters (`locals_max_length`, `locals_max_string`, `locals_max_depth`, `locals_hide_dunder`, `locals_hide_sunder`) silently ignored because `__init__` forwarded only `show_locals` to `self.extract(...)`. Furthermore, recursive `ExceptionGroup` unwrapping on Python 3.11 dropped string and depth limits. **This bug was still unpatched in `rich` today.** We verified it with a standalone reproduction script and reported the details on [Textualize/rich#3301](https://github.com/Textualize/rich/issues/3301).

---

## What a Type Checker Does—and What It Misses

A natural question is: *why not just run `mypy` or `pyright` in a hook or CI check?*

To answer that rigorously, we built an automated stale-file recall harness (`experiments/recall_mutation/typecheck_baseline.py`) and ran `mypy 2.4.1` on 428 ground-truth mutation sites across the evaluated repositories.

The results showed a clear architectural division:

1. **Where `mypy` wins:** For broken calls to removed or renamed functions, `mypy` catches the broken top-of-file import in typed codebases. In our recall corpus, `mypy` reported 70 of 81 unique-name breaking/removed sites, compared to 67 for `mipdelta`. (At the exact call line, `mypy` reported only 8 of 81, because flagging the import binds the name to `Any` and silences errors at downstream function calls; `mipdelta` names each downstream call site directly.)
2. **Where `mypy` is blind:** Every single one of the real upstream omissions we found in `httpx`, `paramiko`, `scrapy`, and `rich` was **100% type-correct Python**. Omitting an optional keyword parameter (`http1=...`, `disable_algorithms=...`, `locals_max_string=...`) from a wrapper or override is valid typing. Unit tests passed because existing tests exercised the default path. `mypy` recall on those real upstream bugs was **0% by definition**.

In short: type checkers enforce static type contracts. They do not enforce **semantic propagation**—ensuring that a parameter accepted at layer A actually reaches the sink at layer B.

---

## What We Learned from a $2.31 Live Pilot

With real upstream omissions in hand, we designed a live pilot using `claude-opus-5-5` running headlessly in isolated repository clones. We built tickets matching the exact omissions that human maintainers of `rich` and `httpx` had shipped.

The pilot plan included a strict stopping rule: if the unaided frontier model did not make the omission on baseline tasks, stop immediately rather than spending the $50 evaluation budget.

We ran Stage 1 (six episodes, two per task, unaided with standard tools and no hooks). Two independent graders evaluated the resulting patches blind.

**The unaided model covered the missing forwarding in 6 out of 6 episodes.** 
Total spend: **$2.31**.

Why did the model succeed where human maintainers slipped? In a focused 1-minute edit on a short ticket, a frontier model proactively grepped for callers of the function it was editing before declaring victory. It didn't experience the context fatigue, split attention, or multi-day PR sprawl that human maintainers face.

That was a clarifying result: **the primary failure mode is not a frontier model on a 1-minute, single-ticket task.** 

The failure mode occurs when humans or agents work across **long, multi-file sessions, refactorings, and PR boundaries** where subtle optional parameter drops and unawaited coroutines pass code review and green CI checks unnoticed.

---

## Where This Leaves MipCode

We started this project trying to invent an interactive graph search tool to beat `ripgrep`. We failed, because search was not the bottleneck.

We pivoted to building a post-edit contract check for agents. In doing so, we found that frontier models on small tasks often check their own callers—but human maintainers and agents across real codebases routinely ship silent, type-correct contract omissions that neither type checkers nor existing test suites catch.

Today, `mip-delta` is focused on that exact gap:
- **As a pre-commit, pull request, and merge check (`mip-delta --check-git`):** A fast (~5 ms, zero-dependency) AST analysis that flags parameter sinks, delegating wrapper drops, subclass override mismatches, and async/exception leaks on diffs.
- **As an agent completion guardrail (`ObligationLedger`):** An AST-anchored, line-independent state machine that hooks into `PostToolUse` and `Stop` events, ensuring an agent cannot declare victory while open contract obligations remain unaddressed or unwaived.

The code, reproducible replay harnesses, and adjudication data are open source at [github.com/Nitefawkes/mipcode](https://github.com/Nitefawkes/mipcode).
