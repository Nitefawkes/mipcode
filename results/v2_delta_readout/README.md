# v0.6 research evidence

These reports and data accompany the [engineering article](../../docs/engineering_blog_post_mipdelta.md).

| Study | Report | Data | Scope |
| --- | --- | --- | --- |
| Local edits | [Report](natural-local-corpus-v2.md) | [JSON](natural-local-corpus-v2.json) | 50 selected changes to single function bodies |
| Historical replay | [Report](historical-replay-v2.md) | [JSON](historical-replay-v2.json) | Ten development cases |
| Constructed paired pilot | [Report](live-codex-paired-pilot-v2.md) | [JSON](live-codex-paired-pilot-v2.json) | Six tasks and 12 episodes |
| OSS and fixture pairs | [Report](live-codex-oss-benchmark-v2.md) | [JSON](live-codex-oss-benchmark-v2.json) | Eight tasks and 16 episodes, including constructed packages and supplied stubs |
| Reminder comparison | [Report](live-codex-reminder-ablation-v2.md) | [JSON](live-codex-reminder-ablation-v2.json) | Six new reminder episodes on selected historical A/B outcomes |
| Runtime monitoring | [Report](pytest-sys-monitoring-v2.md) | [JSON](pytest-sys-monitoring-v2.json) | Measured overhead and sampling coverage in three suites |

The [manifest](manifest.json) records original and preview hashes. Preview copies replace machine-local run roots. Raw provider traces and live runner configuration are retained separately.

These reports describe the v0.6.0 engine. Since then, `parameter_truthiness_review` has become [opt-in](../../docs/usage.md#turn-on-an-opt-in-check). It produced two of the ten historical replay findings, both in the T45 invariant case. The current default set reproduces the other eight, and `--enable parameter_truthiness_review` restores all ten.

The paired studies cover selected tasks. Only the reminder arm ran anew in the later comparison. The OSS05 ticket and oracle differ on whether the public exporter must expose the new option. The article explains how those limits affect the results.
