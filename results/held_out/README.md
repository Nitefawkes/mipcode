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

## Notes on the run

- The rich clone's head is a merge commit. The replay's last commit is `364ce24a3be6`, the last non-merge commit.
- The readers and the third read are model judgments made from the commit diffs and the source. The rich `Traceback` finding was also checked by parsing the file at the replayed head.
- [Replay data](replay/) holds one report per repository. [adjudication.json](adjudication.json) holds every site with both readers' labels, their reasons, and the final label.
