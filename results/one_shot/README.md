# Live one-shot briefing pilot

Three constructed Python tasks, one episode per arm, independently reviewed in a separate model session. The [public summary](summary.json) includes the protocol settings, task review, and per-episode measurements. It links back to the retained evidence by SHA-256. Raw provider traces and machine-local paths stay in the original evidence bundle.

| Arm | Accepted | Total input + output tokens | Cached input | Tool calls | Tool response bytes | Wall seconds including brief |
|---|---:|---:|---:|---:|---:|---:|
| none | 3/3 | 288,579 | 263,936 | 25 | 15,193 | 78.39 |
| map | 3/3 | 322,371 | 270,592 | 28 | 14,701 | 88.65 |
| lod | 3/3 | 317,516 | 270,848 | 27 | 10,559 | 86.16 |

| Task | No brief | Map | LOD |
|---|---|---|---|
| LP01 | pass, 91,656 tokens | pass, 122,970 tokens | pass, 104,650 tokens |
| LP02 | pass, 108,542 tokens | pass, 92,035 tokens | pass, 105,880 tokens |
| LP03 | pass, 88,381 tokens | pass, 107,366 tokens | pass, 106,986 tokens |

Provider token totals include cached input and are a usage measure. Reviewer and calibration usage are separate. The study ran one episode per task and arm on constructed fixtures.

All nine episodes passed both acceptance suites and transport reconciliation. Semantic LOD returned 30.5% fewer tool bytes than no brief and 28.2% fewer than the map. It used 10.0% more total tokens than no brief and 1.5% fewer than the map. Neither brief improved accepted patches on this cohort. One run per task and arm limits inference about model variation.

## Offline mechanism check

On four separately constructed fixtures, the no-brief, simple-map, and Semantic LOD arms exposed 0/20, 7/20, and 20/20 required physical lines at startup. The earlier one-hop compiler exposed 14/20 on the same fixtures. The offline and live cohorts use different tasks. The [offline protocol](../../experiments/one_shot/README.md) reproduces the delivery checks.

All live arms used the same post-edit advisory, with runtime observation disabled. The paired structured briefs shared a 1,024-byte ceiling. Each episode had ten admitted tool calls, 16,000 cumulative tool-response bytes, a 180-second timeout, and a 350-word final answer cap. Token totals include cached input and do not estimate charges.
