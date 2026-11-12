# Regression check after the detector fixes

This is a regression check. It is not a third validation round. The fixes in `867f274`, `7ed9e0e`, and `f4361fd` were written after reading the [round 1](../README.md) and [round 2](../round2/README.md) results, so these ten repositories no longer count as unseen for the detectors that changed. The check answers one question: did the fixes remove the false reports they targeted without removing anything a reader had judged useful?

The engine is `src/mipdelta` at commit `f4361fd`, with its default detector set: `removed_callable_review` is on again and `callback_binding_review` stays off. Each repository was replayed over the same 800 commits as before.

## Result

No site that a reader labeled a real omission or review-worthy was lost, and no new site appeared.

| | Round 1 repositories | Round 2 repositories |
| --- | ---: | ---: |
| Contract-changing commits | 478 | 567 |
| Silent before the fixes, same detectors | 452 (94.6%) | 549 (96.8%) |
| Silent after | 456 (95.4%) | 552 (97.4%) |
| Reported sites before, same detectors | 56 | 161 |
| Reported sites after | 33 | 156 |
| Sites removed | 23 | 5 |

"Same detectors" means the earlier replay with the `callback_binding_review` sites left out, so that both columns cover the detectors that are on now.

### What was removed

| Cause fixed | Sites removed | Their labels |
| --- | ---: | --- |
| `removed_callable_review`: the name was still bound | 25 | 24 false reports. One was not in the round 2 sample; it is a second call to the same aliased function in the same pytest commit as a site labeled false |
| `call_signature_review`: an async generator read as a coroutine | 2 | 2 false reports |
| `override_signature_review`: a default moved into a constant of equal value | 1 | 1 false report |

Three other scrapy sites kept their place and changed message. A generator became an async generator, and the report now says so. Readers had labeled two of them review-worthy and one false.

### What remains

| | Round 1 repositories | Round 2 repositories |
| --- | ---: | ---: |
| Sites labeled real omission | 10 | 82 |
| Sites labeled review-worthy | 4 | 3 |
| Sites labeled false report | 19 | 20 |
| Sites not in the reading sample | 0 | 51 |
| Flagged commits | 22 | 15 |
| Flagged commits with a real omission | 8 | 5 |
| Flagged commits with a review-worthy site and no real omission | 3 | 2 |
| Flagged commits with only false reports | 11 | 8 |

In round 2, 129 of the 156 sites are `removed_callable_review` reports from two paramiko commits that deleted a module and left its importers unchanged. The 51 unread sites are all from the first of those commits, where all 77 sites that were read are real. Site counts for this detector follow whichever kind of commit a repository contains, so the per-commit rows describe the check better than the per-site rows.

Pooled over both sets, 1,008 of 1,045 contract-changing commits are silent (96.5%), and 18 of the 37 flagged commits contain a real omission or a review-worthy site.

## Limits

- **These numbers are fitted.** The fixes were written against these false reports. A fresh set of repositories would show more causes that are not handled, as round 1 did after tuning on the first five.
- **The new rules are narrow on purpose.** A removed name counts as still bound only when its own scope assigns it from an expression that could be a callable. A class renamed to a name that was already its alias keeps its methods. A class renamed with no alias still reports callers of the old name.
- **An independent review found defects in the first version of these fixes,** which `f4361fd` corrects. The first version treated any binding of the name as "still bound", including a literal or a `del`, and could stop the analysis on a very large constant expression. The review and its cases are described in that commit and in `tests/test_held_out_false_reports.py`.
- **Labels are the earlier readers' labels.** No site was read again for this check.

## Data

- [round1/](round1/) and [round2/](round2/) hold one replay report per repository.
- [round1-compare.json](round1-compare.json) and [round2-compare.json](round2-compare.json) list the removed, kept, and added sites with their earlier labels. They were made with [compare_replays.py](../../../experiments/held_out/compare_replays.py).
