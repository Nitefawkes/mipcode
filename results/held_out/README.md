# Held-out validation results

These results follow the [plan](../../experiments/held_out/README.md) that was committed before any of these repositories was analyzed. The engine is `src/mipdelta` at commit `c3ccbb624fed`, with the default detector set. No detector was changed in response to anything reported here.

## Study 1: silence on complete changes

**Outcome: partial.** Pooled silence is 93.1%, above the 93.0% bar. attrs is at 86.5%, below the 88.0% floor for a single repository, so the result does not meet the rule for "generalizes".

| Repository | Commits changing production Python | Contract-changing | Silent | Reported sites |
| --- | ---: | ---: | ---: | ---: |
| attrs | 184 | 52 | 45 (86.5%) | 24 |
| httpx | 409 | 128 | 119 (93.0%) | 22 |
| marshmallow | 428 | 82 | 78 (95.1%) | 4 |
| rich | 395 | 63 | 58 (92.1%) | 8 |
| mkdocs | 435 | 153 | 145 (94.8%) | 10 |
| **Pooled** | 1,851 | 478 | 445 (93.1%) | 68 |

Each repository contributed its most recent 800 non-merge commits. No commit failed analysis, and no local commit was flagged.

The check reports more often here than on the tuned repositories: 14.2 sites per 100 contract-changing commits, against 3.5 in the [tuned replay](../history_replay/README.md). The tuned figure of 97.8% silence overstated how quiet the check is on code it has not seen.

### What the reports were

Two readers classified all 68 sites independently. The plan allowed a sample of 60, and reading all of them removed the sampling step. The readers agreed on 64 sites (94.1%, Cohen's kappa 0.84). A third read resolved the other four.

| Label | Sites | Share |
| --- | ---: | ---: |
| Real omission | 10 | 14.7% |
| Review-worthy | 5 | 7.4% |
| False report | 53 | 77.9% |

Of the 33 flagged commits, 8 contain a real omission, 4 more contain a review-worthy site, and 21 contain only false reports.

| Detector | Real omission | Review-worthy | False report |
| --- | ---: | ---: | ---: |
| `delegating_wrapper_review` | 6 | 0 | 12 |
| `override_signature_review` | 2 | 3 | 5 |
| `parameter_sink_review` | 1 | 1 | 0 |
| `removed_callable_review` | 1 | 0 | 23 |
| `callback_binding_review` | 0 | 1 | 11 |
| `exception_contract_review` | 0 | 0 | 2 |

### The real omissions

These are the first sites from any MipCode study that were confirmed as omissions in upstream code. In each one, a commit added a parameter to one layer and did not carry it through a sibling or inner layer.

| Site | What the commit left undone | Later upstream fix |
| --- | --- | --- |
| httpx `AsyncClient._init_proxy_transport` | Accepts the new `http1` parameter and does not pass it on. The sync client does. | `1a66014` (#3023) |
| httpx `Client.__init__`, `AsyncClient.__init__` (2 sites) | The commit adds a client-level redirect setting to the base class only. The public constructors do not accept it. | `47266d7` (#1808) |
| mkdocs `SubConfig.__init__` | Does not pass `config_file_path` to the base constructor. | `66f8df9` |
| mkdocs `gh_deploy` call to `ghp_import` | The rewritten call passes `push=force`, so a plain deploy stops pushing. | `2688a6d` |
| rich `Traceback.__init__` (3 sites) | Stores the new locals options and calls `extract` with `show_locals` only. | None found. The call still passes only `show_locals` at the replayed head. |
| rich `set_cell_size` (2 sites) | Measures width without the `unicode_version` it was given. Latent: no in-tree caller passes a version. | None found |

The mkdocs `ghp_import` site was right for a reason the check did not state. It reported a removed declaration, and the defect was in the arguments of the rewritten call.

### The false reports

The readers found the same causes independently.

- **A name that is still bound (23 sites, `removed_callable_review`).** The definition moved or was rebound, and the check read it as removed. Examples: nested functions that became values returned by a helper and bound by tuple unpacking, a method replaced by an instance attribute set in `__init__`, a module-level alias that remained after a stub was deleted, and methods of a class that was renamed.
- **A name resolved to the wrong thing (6 sites).** A local variable named like a changed function, and a class whose base is the standard library's `Request` matched against the project's own `Request`.
- **A wrapper that is right not to forward (12 sites, `delegating_wrapper_review`).** The wrapper applies the parameter itself, or the parameter means something different in the callee, or the replaced call did not forward it either.
- **A callable passed as a value after an optional parameter was added (most `callback_binding_review` sites).** The binding is unaffected by construction.
- **An exception that already bypassed the handler before the change (2 sites).**

The first two causes are resolution limits. The tuned replay had removed several forms of them, and these are different forms of the same problem. That is what fitting to five repositories looks like.

### What the horizon column was worth

The replay records whether a report disappeared within 25 commits. The readers found that signal unreliable. Most "resolving" commits were unrelated edits, and the genuine upstream fixes above arrived after the 25-commit window or have not arrived.

## Study 2: recall on constructed omissions

The [stale-file mutation harness](../../experiments/recall_mutation/README.md) built 81 mutations from 63 commits, with 205 ground-truth sites. No mutation failed analysis, and no finding appeared outside a stale file.

**Outcome on the primary measure: 5 of 6 sites (83%), above the 70% bar, on a sample too small to support a conclusion.** Only four commits had a call that cannot bind to the new signature and a function name defined once. The larger strata are more informative.

| Stratum | Sites | Reported by `mip-delta` | Commits (with a report) |
| --- | ---: | ---: | ---: |
| Call cannot bind, name unique (primary) | 6 | 5 (83%) | 4 (3) |
| Definition removed, name unique | 75 | 62 (83%) | 28 (22) |
| Call cannot bind, name ambiguous | 55 | 6 (11%) | 5 (1) |
| Definition removed, name ambiguous | 18 | 4 (22%) | 3 (2) |
| Call still binds | 48 | 4 (8%) | 27 (3) |
| Call uses unpacking | 3 | 0 | 3 (0) |
| **All** | 205 | 81 (40%) | 63 (29) |

### Recall follows the form of the call

Across the sites where the call cannot bind or the definition was removed:

| Call form | Reported | Missed |
| --- | ---: | ---: |
| Bare name, `f(x)` | 68 | 20 |
| Attribute, `obj.f(x)` or `module.f(x)` | 9 | 57 |

In the unique-name strata the split is complete. All 67 bare-name sites were reported and all 14 attribute sites were missed. The misses include method calls on an instance attribute (`self._timer.sync_start()`), on a parameter (`site_navigation.walk_pages()`), and calls through a module (`utils.urlquote(url)`). A quiet result therefore says little about method calls and module-qualified calls.

The ambiguous rows are weaker evidence. All 55 sites where the call cannot bind and the name is ambiguous come from httpx, and 32 of them from one commit that changed several methods named `request`. Matching by name alone attaches each call to every one of them.

### What the check adds to mypy

mypy 2.4.1 ran on the same 81 mutated trees with a fixed configuration: no project settings, no installed dependencies, `--check-untyped-defs`. All 205 sites completed. A site counts for mypy when a new error is on the call's lines or on the stale file's import of the name, as fixed in the plan before this run.

| Stratum | Sites | Both | `mip-delta` only | mypy only | Neither |
| --- | ---: | ---: | ---: | ---: | ---: |
| Cannot bind or removed, name unique | 81 | 65 | 2 | 5 | 9 |
| Cannot bind or removed, all | 154 | 66 | 11 | 29 | 48 |
| All sites | 205 | 68 | 13 | 31 | 93 |

On these omissions mypy reports as much as `mip-delta` or more: 70 of 81 against 67 of 81 in the unique-name strata, and 95 of 154 against 77 of 154 overall. `mip-delta` alone reports 2 of the 81 unique-name sites.

Counting only errors on the call's own lines, mypy reports 8 of those 81 sites. The difference is where each tool points. For a removed name, mypy reports the import and is silent at the call, and `mip-delta` reports each call.

All five repositories are typed, which favors mypy. A project's own mypy run, with its settings and dependencies installed, would see more than this one does.

## Reading the two studies together

- **Where a type checker already covers the ground, the check adds little.** Stale calls to changed or removed functions are what the recall corpus contains, and mypy reports about as many of them.
- **The real omissions in Study 1 are a kind a type checker does not report.** A new optional parameter that one layer accepts and does not forward is type-correct. Nine of the ten real omissions came from `delegating_wrapper_review`, `override_signature_review`, and `parameter_sink_review`.
- **The two noisiest detectors overlap most with the type checker.** `removed_callable_review` produced 23 of the 53 false reports in Study 1 and 67 of the 81 recall hits here, nearly all of which mypy also reports. `callback_binding_review` was wrong on 11 of 12 sites.
- **Name resolution is the limit on both sides.** It reads a rebound or renamed name as removed, and it does not follow method or module-qualified calls.

These readings come from five typed repositories and constructed omissions. They describe where to look next. They are not a measurement of how the check behaves in an agent's session.

## Notes on the run

- The rich clone's head is a merge commit. The replay's last commit is `364ce24a3be6`, the last non-merge commit.
- The readers and the third read are model judgments made from the commit diffs and the source. The rich `Traceback` finding was also checked by parsing the file at the replayed head.
- [Replay data](replay/) holds one report per repository. [adjudication.json](adjudication.json) holds every site with both readers' labels, their reasons, and the final label.
- [Recall corpora](recall/) hold every mutation and site with its strata and `mip-delta` result. [mypy results](typecheck/) hold the same sites with mypy's new errors, the exact command, and the mypy and Python versions.
- The recall harness and the mypy baseline were built and debugged on the five tuned repositories. Each ran once on the held-out repositories.
