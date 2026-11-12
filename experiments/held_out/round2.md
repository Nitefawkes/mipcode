# Held-out validation plan, round 2

This plan was written and committed before any of its repositories was analyzed. It tests one change that the [first round](../../results/held_out/README.md) suggested.

## The change under test

`removed_callable_review` and `callback_binding_review` are now opt-in. In round 1 they produced 36 of the 68 reported sites, and 34 of those 36 were false. The other detectors produced 32 sites, of which 9 were real omissions and 4 were review-worthy.

That reading was made after seeing the round 1 results, so round 1 cannot confirm it. This round uses five repositories that no earlier study or round used.

No other detector or resolver change is made. The engine is `src/mipdelta` at the commit that contains this plan. If anything is changed in response to this round, it needs another fresh set.

**Known cost.** On the ten historical cases, the default set now reports five findings instead of eight. The three it drops are the adjudicated werkzeug `dump_header` defect in case T11-B, which `callback_binding_review` found.

## Repositories

| Repository | Head | Python files | Why |
| --- | --- | ---: | --- |
| [requests](https://github.com/psf/requests) | `3c7baaa05ab4` | 37 | Mostly unannotated library |
| [urllib3](https://github.com/urllib3/urllib3) | `6da1f82193a6` | 83 | Typed library |
| [pytest](https://github.com/pytest-dev/pytest) | `fc9a7ef678f6` | 274 | Large typed framework with plugins and hooks |
| [scrapy](https://github.com/scrapy/scrapy) | `f9d89010f691` | 543 | Large framework, partly annotated |
| [paramiko](https://github.com/paramiko/paramiko) | `0e9ccb91e0f9` | 70 | Unannotated library |

Selection rules, applied before cloning:

- Not used in any earlier study or round, and not maintained by the same group as one that was. That excludes the twelve repositories already used and the other Pallets, Encode, Textualize, and attrs-family projects.
- At least two codebases with few type annotations, because round 1 used typed repositories only and that favors a type checker.

Each is a shallow clone. The replay covers the most recent 800 non-merge commits of each, with a 25-commit horizon. Only the commit count and file count of each clone were read before this plan was committed.

## Configurations

Both run on the same commits.

- **Default:** the narrowed default set.
- **Enabled:** the default set plus `removed_callable_review` and `callback_binding_review`. This is the round 1 configuration, and it shows what the narrowing removed on fresh data.

`parameter_truthiness_review` stays off in both.

## Study 1: silence and what the reports are

Every reported production site in the enabled configuration is adjudicated by two independent readers with the round 1 labels and rules, and a third read resolves disagreements. The default configuration's sites are a subset. If there are more than 120 sites, a random sample of 120 stratified by detector is read (seed `20261113`), and it includes every default-configuration site.

| Question | Measure | Confirmed when | Refuted when |
| --- | --- | --- | --- |
| Is the narrowed default set quiet on unseen code? | Pooled silence, default configuration | At least 95.0%, and every repository with 30 or more contract-changing commits at least 90.0% | Pooled silence below 93.0% |
| Are its reports worth reading? | Share of default-configuration sites labeled real omission or review-worthy | At least 40% | Below 25%, which is no better than round 1 overall |
| Were the two detectors rightly removed? | Share of their sites labeled false report | At least 70% | Below 50% |
| Does narrowing lose real omissions? | Real omissions reported only by the two opt-in detectors | Reported as a count, with each one described | |

A result between "confirmed" and "refuted" is reported as inconclusive. With fewer than 15 default-configuration sites, the second question is reported as too small to judge.

## Study 2: recall and the mypy overlap

The [stale-file mutation harness](../recall_mutation/README.md) and the mypy baseline run unchanged, in both configurations, with the comparison rule fixed in the round 1 plan.

| Question | Measure | Expectation from round 1 |
| --- | --- | --- |
| What does the narrowed set still find among constructed omissions? | Site recall where the call cannot bind and the name is unique | Reported. The stratum had six sites in round 1 |
| What does narrowing give up? | Site recall on removed definitions, default against enabled | Near zero by default, since `removed_callable_review` found these |
| Does mypy cover what was given up? | Sites reported by the enabled configuration only, as a share of unique-name sites | At most 10%. Round 1 gave 2 of 81 |
| Does that hold on unannotated code? | The same share for requests and paramiko alone | Unknown. This is the reason those repositories are in the set |

If mypy misses many of these sites on the unannotated repositories, the two detectors keep a use there, and the documentation should say so.

## Use of the results

Real omissions found in this round are candidate tasks for the live pilot, because the task and its oracle come from upstream history and not from us.

## Limits known in advance

- Five repositories per round is a small sample. Two rounds do not make the sample representative.
- The labels are model judgments from the commit diffs and source.
- The constructed omissions are signature changes and removals. The omissions that mattered in round 1, a new optional parameter that is not forwarded, are not in the recall corpus because the old call still binds.
