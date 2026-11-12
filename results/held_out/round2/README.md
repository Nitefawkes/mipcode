# Held-out validation results, round 2

These results follow the [round 2 plan](../../../experiments/held_out/round2.md), committed with the change it tests in `21c9341` before any of these repositories was analyzed. The engine is `src/mipdelta` at that commit. Nothing was changed in response to what is reported here.

The round tested one change: `removed_callable_review` and `callback_binding_review` became opt-in. Two of the plan's three predictions were refuted.

| Question | Result | Outcome |
| --- | --- | --- |
| Is the narrowed default set quiet on unseen code? | 552 of 567 contract-changing commits silent (97.4%). The lowest repository is urllib3 at 95.5% | Confirmed |
| Are its reports worth reading? | 7 of 30 sites are real omissions or review-worthy (23%) | Refuted. The bar was 40%, and below 25% counts as no better than round 1 |
| Were the two detectors rightly removed? | 12 of 90 sampled sites are false reports (13%) | Refuted as a pair. The plan expected at least 70% |
| Does narrowing lose real omissions? | Yes: two paramiko commits, 129 sites | See below |

The two detectors behaved differently, so the third answer hides more than it shows. `callback_binding_review` was false on all 11 sampled sites. `removed_callable_review` was right on 78 of 79, and 128 of its 131 sites come from a single commit.

## Study 1: silence and what the reports are

| Repository | Contract-changing commits | Silent, default set | Sites, default set | Sites, with both detectors enabled |
| --- | ---: | ---: | ---: | ---: |
| requests | 29 | 29 (100%) | 0 | 0 |
| urllib3 | 111 | 106 (95.5%) | 6 | 8 |
| pytest | 116 | 115 (99.1%) | 2 | 6 |
| scrapy | 218 | 211 (96.8%) | 15 | 21 |
| paramiko | 93 | 91 (97.8%) | 7 | 145 |
| **Pooled** | 567 | 552 (97.4%) | 30 | 180 |

With both detectors enabled, pooled silence is 94.4%. No commit failed analysis in either configuration.

### Adjudication

The enabled configuration reported 180 sites, so the plan's sampling rule applied: 120 sites, stratified by detector, including all 30 default-set sites (seed `20261113`). Two readers classified each site independently. They agreed on 114 (95.0%, Cohen's kappa 0.89), and a third read resolved the other six. All six disagreements were between review-worthy and false report.

| Detector | Sites read | Real omission | Review-worthy | False report |
| --- | ---: | ---: | ---: | ---: |
| `parameter_sink_review` | 11 | 3 | 0 | 8 |
| `call_signature_review` | 9 | 1 | 2 | 6 |
| `state_field_review` | 4 | 0 | 0 | 4 |
| `override_signature_review` | 3 | 0 | 0 | 3 |
| `delegating_wrapper_review` | 2 | 0 | 0 | 2 |
| `exception_contract_review` | 1 | 0 | 1 | 0 |
| **Default set** | 30 | 4 | 3 | 23 |
| `removed_callable_review` (opt-in) | 79 of 131 | 78 | 0 | 1 |
| `callback_binding_review` (opt-in) | 11 of 19 | 0 | 0 | 11 |

Of the 15 commits the default set flagged, 3 contain a real omission and 2 more contain a review-worthy site.

### Real omissions in the default set

| Site | What the commit left undone | Later upstream fix |
| --- | --- | --- |
| paramiko `SSHClient.connect` and `Transport.__init__` (2 sites) | `disable_algorithms` is accepted and documented and never used. The readers gave medium confidence, because the commit was a declared signature-only step | `dceaa61d`, `b2cb9c1a`, two to three commits later |
| paramiko `AgentKey.sign_ssh_data` | The new `algorithm` argument is ignored, so the agent still signs with SHA-1 when SHA-2 was asked for | `2b666256`, the next relevant commit |
| scrapy `FeedExporter.item_scraped` | `_close_slot` became `async`. This caller then stored an unstarted coroutine, so full batches were no longer written during the crawl | `b2d4eedea` (#7730), after the 25-commit window |

### What the opt-in detectors reported

- **`removed_callable_review`.** One paramiko commit deletes `py3compat.py` and leaves every importer unchanged. Its message says the imports are not fixed yet and CI will break. The detector reported 128 calls in those files, and all 77 that were sampled are real. Each file has one cause, its import line. A second paramiko commit deletes `setup_helper.py` while `setup.py` still imports it. The one false report is a pytest function that became an alias.
- **`callback_binding_review`.** Every sampled site is wrong for the same reason as in round 1: a function passed as a value where the signature change cannot matter. Six are invoke tasks being registered in a `Collection`.

Across both rounds `callback_binding_review` reported 23 sites that were read, and 22 were false. Round 1's reading of `removed_callable_review`, 23 false of 24, did not hold. Its false reports come from names that were rebound, renamed, or moved. Its true reports come from a module or function that was deleted while callers remained. Site counts for it are dominated by whichever kind of commit a repository happens to contain.

### False reports in the default set

- **A site the same commit already updated.** The most common cause: the check reports a call or override that the commit itself made consistent.
- **A parameter that is unread by design.** An override that takes a parameter only to match its base, a deprecated argument handled by a decorator, or a signature required by a framework.
- **A detector bug.** `call_signature_review` reported "target became async" for a function that became an async generator. Calling it returns an async iterator, not a coroutine (2 sites).
- **`state_field_review`** paired a new counter with an unrelated queue attribute (4 sites).
- **`override_signature_review`** compared `2**16` with a constant defined as `2**16` (1 site).

The detectors that produced most of round 1's real omissions did not repeat. `delegating_wrapper_review` and `override_signature_review` had 5 sites here, all false. Round 1's reading that these three detectors are where the value lies was drawn after the fact, and it holds here only for `parameter_sink_review`.

## Study 2: recall and the mypy overlap

The harness built 86 mutations from 52 commits, with 223 ground-truth sites. requests produced none. No mutation failed analysis, no finding appeared outside a stale file, and mypy completed on every site.

| Stratum | Sites | Commits | Default set | Both detectors enabled | mypy |
| --- | ---: | ---: | ---: | ---: | ---: |
| Call cannot bind, name unique (primary) | 21 | 9 | 10 (48%) | 10 (48%) | 21 (100%) |
| Definition removed, name unique | 53 | 21 | 0 | 38 (72%) | 28 (53%) |
| Call cannot bind, name ambiguous | 17 | 3 | 0 | 0 | 2 |
| Definition removed, name ambiguous | 47 | 4 | 0 | 15 (32%) | 12 |
| Call still binds | 83 | 18 | 4 (5%) | 4 (5%) | 3 |
| Call uses unpacking | 2 | 2 | 0 | 0 | 0 |
| **All** | 223 | 52 | 14 (6%) | 67 (30%) | 66 (30%) |

- **Primary measure.** The default set reports 10 of 21 sites where the call cannot bind and the name is unique. mypy reports all 21. Round 1 had six such sites.
- **What narrowing gives up.** As expected, the default set reports none of the removed-definition sites.
- **Call form.** In the unique-name strata, bare-name calls were reported 42 of 52 times and attribute calls 6 of 22 times. Round 1's split was complete; this one is not, and it points the same way.

### Does mypy cover what was given up?

In the unique-name strata (74 sites), the enabled configuration and mypy each report about two thirds, and they are not the same two thirds.

| | Sites |
| --- | ---: |
| Both | 29 |
| Enabled configuration only | 19 |
| mypy only | 20 |
| Neither | 6 |

The plan expected at most 10% to be reported by the enabled configuration only. The result is 19 of 74 (26%), so that expectation was refuted.

It needs a caveat about the baseline. 18 of the 19 come from three commits that deleted or renamed a whole module while the stale file still imports from the old one: paramiko's `ssh_gss`, and two changes to urllib3's test-server modules. The baseline runs mypy with `--ignore-missing-imports`, because the projects' dependencies are not installed, and that flag also silences an import of a module missing from the project itself. A project's own mypy run would report that import. So these sites show a limit of this baseline more than a limit of mypy.

**Unannotated code.** The plan asked whether the answer differs where annotations are few. requests gave no mutations. paramiko gave 14 unique-name sites from two commits: the enabled configuration reports 12 and mypy 3. Nine of those are the `ssh_gss` deletion above. That is too little, and too tied to one commit, to answer the question.

## Reading the two rounds together

Ten repositories, 1,045 contract-changing commits, with every configuration fixed before its run.

- **The default set is quiet.** 93.1% of contract-changing commits were silent in round 1 with all detectors on, and 97.4% in round 2 with the narrowed set.
- **About a third of what it reports is worth reading.** Counting only the detectors now in the default set: 13 real omissions and 7 review-worthy sites out of 62 across both rounds. It found a real omission that upstream later fixed, or has not yet fixed, in 10 commits.
- **Every one of those real omissions is type-correct code,** so a type checker would not report them: a parameter accepted and not used or forwarded, a constructor that does not expose a base-class option, a coroutine stored and not started.
- **On stale calls to changed or removed functions, mypy reports about as much.** The check's own share is small in round 1 and mostly an artifact of the baseline's configuration in round 2.
- **Which detector carries the value is not stable between rounds.** Only `parameter_sink_review` found real omissions in both. Ten repositories are too few to rank the others.
- **`callback_binding_review` is noise in both rounds.** Keeping it opt-in is supported.
- **`removed_callable_review` is unresolved.** It is wrong when a name is rebound and right when a module is deleted, and both happen. A module deletion with stale imports is also what a test run or a type checker reports first.

These are readings of upstream history. They say what the check reports on commits that people wrote and reviewed. They do not say what it does for an agent mid-task, which is what the pilot is for.

## Notes on the run

- Each repository contributed its most recent 800 non-merge commits.
- The readers and the third read are model judgments made from the commit diffs and the source.
- [Replay data](replay/) holds one report per repository and configuration. [adjudication.json](adjudication.json) holds each sampled site with both readers' labels, their reasons, and the final label, and [sample-ids.json](sample-ids.json) lists the sample.
- [Recall corpora](recall/) and [mypy results](typecheck/) hold every mutation and site. mypy ran once, on the trees from the enabled-configuration corpus; the ground-truth sites are the same in both configurations.
