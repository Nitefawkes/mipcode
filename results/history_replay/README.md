# History replay over five repositories

This run replays 2,944 upstream commits through `mip-delta` with no model calls. It measures how often the check reports something on a change that upstream reviewed and merged. The [protocol](../../experiments/history_replay/README.md) describes the method and its limits.

The replay does not measure recall. It contains no known omissions, so it cannot show that the check finds real ones. The [paired agent studies](../v2_delta_readout/README.md) remain the evidence for that.

## Repositories

Shallow local clones, replayed to the listed head with a 25-commit horizon.

| Repository | Head | Commits replayed | Commits changing production Python |
| --- | --- | ---: | ---: |
| [click](https://github.com/pallets/click) | `9f0bcdf0d868` | 1,020 | 457 |
| [packaging](https://github.com/pypa/packaging) | `ae4946b30f8a` | 349 | 261 |
| [platformdirs](https://github.com/tox-dev/platformdirs) | `f9323b0d6836` | 349 | 129 |
| [tomlkit](https://github.com/python-poetry/tomlkit) | `d7def7dbcc59` | 410 | 260 |
| [werkzeug](https://github.com/pallets/werkzeug) | `0d35bad79a9f` | 816 | 546 |

## Result

The first replay ran before any change made in response to it. The second ran on the same commits after those changes.

| Measure | First replay | After changes |
| --- | ---: | ---: |
| Contract-changing commits | 464 | 459 |
| Silent | 397 (85.6%) | 438 (95.4%) |
| With at least one reported production site | 67 | 21 |
| Local commits with a reported site | 5 | 1 |
| Reported production sites | 457 | 35 |

The count of contract-changing commits fell by five because `raise self.factory(...)` no longer counts as raising a named exception class.

| Detector | Sites, first replay | Sites, after changes | Commits, after changes |
| --- | ---: | ---: | ---: |
| `parameter_sink_review` | 211 | 0 | 0 |
| `removed_callable_review` | 182 | 1 | 1 |
| `callback_binding_review` | 28 | 10 | 6 |
| `parameter_truthiness_review` | 19 | 19 | 13 |
| `exception_contract_review` | 7 | 0 | 0 |
| `state_field_review` | 5 | 1 | 1 |
| `delegating_wrapper_review` | 3 | 3 | 2 |
| `override_signature_review` | 2 | 1 | 1 |

The pinned checks from the earlier studies were rerun after the changes. The 50-commit local corpus still produces no findings, and the ten historical cases still produce the same ten adjudicated findings. Warm write latency on the local corpus was 4.8 ms median and 28.8 ms at the 95th percentile, against 5.2 ms and 25.0 ms for the engine before these changes on the same machine.

## What the first replay showed

Two detectors produced 86% of the reported sites, and reading the commits showed recurring causes.

- **Unread new parameter.** Most reports were signatures fixed by something other than the function body: abstract and protocol declarations, `__exit__` and similar protocol methods, nested callbacks, trivial hooks that subclasses override, renamed parameters, `*args` and `**kwargs`, and parameters consumed by the paired `__new__` or `__init__`. Every parameter of a newly added function also counted as new.
- **Removed callable.** Reports came from classes that moved to another file with their callers, methods a class still inherits, nested functions extracted to module level, bare names resolved to another function's nested helper, and modules that became packages which re-export the name.
- **Callback binding.** A parameter or local variable with the same name as a changed function was read as a reference to it.
- **Exception contract.** `raise self.parse_error(...)` was read as an exception class named `parse_error`. A new exception raised only when a new optional parameter is supplied was reported at calls that omit the parameter. Two-caller propagation reached handlers through general-purpose methods, where the exception depended on argument values.
- **State field.** A private attribute backing a property of the same name was read as parallel state.

Each cause has a reduced regression test in `tests/test_history_precision.py`.

## What remains

- **`parameter_truthiness_review` (19 sites, 13 commits)** is now the largest source. Upstream code routinely tests a `None`-default parameter by truth value, and for collections and optional strings that is usually intended. No syntactic rule separates those from the constructed case this detector was written for. Whether it belongs in the default set is an open product decision.
- **`callback_binding_review` (10 sites)** reports a changed function that is passed as a value, including two werkzeug commits that changed `dump_header` while it is bound through `header_property`. These are legal code that upstream considered complete. They are the kind of site the detector exists to raise for review.
- The other six sites were not adjudicated.

None of the 35 remaining sites was confirmed as an upstream omission. Thirteen of the truthiness sites show a later edit to the enclosing function, but the reported site is the changed function itself, which is edited again for unrelated reasons. Treat the resolution columns in the per-repository data as leads to read, not as confirmations.

## Data

- [Summary after changes](summary.json) and [first-replay summary](baseline-summary.json). The first replay used an earlier resolution label, `site_edit`, that counted any change to the site's file.
- Per-repository reports after changes: [click](click.json), [packaging](packaging.json), [platformdirs](platformdirs.json), [tomlkit](tomlkit.json), [werkzeug](werkzeug.json). Each lists the flagged commits, their sites, and how later commits resolved them.

## Limits

The five repositories are small, typed, well-tested libraries, and three of them were also used to tune earlier versions of the check. The detector changes were made after reading this run's reports, so the second replay is a fit to these histories and not a held-out measurement. A replay over repositories that were not consulted would test whether the quieter behavior generalizes.
