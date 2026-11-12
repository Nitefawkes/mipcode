# Dogfooding the hooks

This experiment measures what the Claude Code hooks do during real coding sessions: how many sites they open, how those sites leave the ledger, and what each hook invocation costs.

## Enable the hooks

Register both hooks in the repository's `.claude/settings.json` as shown in [Run as Claude Code hooks](../../docs/usage.md#run-as-claude-code-hooks). Logging is always on wherever a ledger exists.

## Where the logs are

Each ledger has an append-only event log beside it. A hook session keeps `.mipdelta/ledger-<session>.json` at the top of the repository, so its events go to `.mipdelta/ledger-<session>.events.jsonl`. A `--check-git --ledger PATH.json` run writes `PATH.events.jsonl`. The `.mipdelta` directory carries a `.gitignore`, so the logs stay out of Git status.

Each line is one JSON object with `event`, `ts`, `invocation`, `session`, and `base_commit`. Post-edit events also carry `edited_path`. The events are `opened`, `discharged`, `waived`, `stop_blocked`, and `run`. A `run` records `source`, `wall_ms`, the `opened`, `discharged`, and `open` counts, the number of changed paths, and `workspace_parsed`. The log holds names, paths, and line numbers. It holds no source text or finding evidence.

A session that never opens a site has no ledger, so by default it leaves no log. Set `MIPDELTA_LOG_ALL_RUNS=1` in the hook's environment to record a `run` for every Python edit. That gives the count and cost of quiet runs, which a dogfooding study needs. `wall_ms` covers the work inside the process. It does not include interpreter startup.

## Summarize

```sh
python experiments/dogfood/summarize_events.py path/to/repo [another/repo ...]
python experiments/dogfood/summarize_events.py path/to/repo --json
```

Pass repository roots or individual `*.events.jsonl` files. The summary is printed per argument and pooled. One event log counts as one session.

A discharge is classified by the file edited in the invocation that discharged it. `site_file_edited` means the site's own file was edited. `other_file_edited` means another file was edited, which includes reverting the contract change. `unknown` means no file was named, as at a stop hook or a `--check-git` run. Edits from open to discharge count post-edit invocations after the one that opened the site.

## What the numbers do not show

A discharge means the finding went away. It is not proof that the edit was right. An edit to the site's file can remove the finding without fixing the problem, and a revert removes it by giving up the change. A site that stays open may be a false report, or it may be real work that was left unfinished. The log cannot tell these apart. Read the session's diff or transcript to decide.
