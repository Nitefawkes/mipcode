# Using mip-delta

Run Git checks in a repository with an existing commit. Use `--base-ref` to compare the working tree with an existing ref.

```sh
mip-delta --check-git --root . --base-ref HEAD --json
```

JSON output includes changed paths, findings, and loaded runtime edges. Compact text defaults to a 250-byte advisory budget. The check normally returns zero when it reports findings. Add `--fail-on-findings` to return status 1 for findings.

Use `--root` to limit the analysis to a subdirectory. Git mode reads tracked and non-ignored untracked Python files. Paths stay relative to the top of the repository so imports and reported locations use the same coordinates. A failed Git command, unreadable source, or syntax error in a changed or proposed Python file returns status 2. Text diagnostics go to stderr. With `--json`, an analysis failure returns an object with `status` set to `"error"` and an `error` message. An unchanged file that does not parse, such as a template or a legacy script, is skipped. It contributes no callers or definitions. Text mode names skipped files on stderr, and JSON lists them in `skipped_unparsed_paths`.

The [bundled example](../README.md#quick-start) uses candidate mode. It compares a proposed replacement with an existing file and reports locations to review.

At resolved calls, the check can flag new required arguments, removed parameters, renamed keywords, changes to positional binding, and changes between synchronous and async definitions. It also reports surviving calls whose previous resolved definition was removed, unless the name is still bound where it was declared. Signature binding uses explicit positional and keyword arguments. Decorators, argument unpacking, dynamic dispatch, and indirect imports can limit coverage. Calls through an attribute, such as `self.client.send()` or `module.helper()`, are resolved less often than calls by bare name.

A newly raised exception is compared with narrow `try`/`except` handlers at the call and at one unguarded caller above it. A handler for a base class counts as catching the exception. Builtin classes and classes defined in the workspace are followed through their bases. The check stays quiet when both the raised class and a handler class come from outside the workspace, because their relationship is unknown.

## Turn on an opt-in check

Three checks are off by default because most of their reports on complete upstream commits were false.

| Check | Reports | Why it is off by default |
| --- | --- | --- |
| `callback_binding_review` | A changed function that is passed as a value | 22 of its 23 held-out reports across [both](../results/held_out/README.md) [rounds](../results/held_out/round2/README.md) were false, mostly a function passed where the signature change cannot matter. |
| `parameter_truthiness_review` | A new or changed `None`-default parameter tested as a bare truth value, as in `if base:` | Upstream code often intends this. It produced the most reports in the [first history replay](../results/history_replay/README.md). |
| `state_field_review` | A new sibling `self.<field>` write parallel to a field read in the same class | 0 of 4 kept reports in the [10-repo regression check](../results/held_out/regression/README.md) were omissions or review-worthy. |

Turn a check on with `--enable`, which can be repeated:

```sh
mip-delta --check-git --enable callback_binding_review
```

`--enable` works with every mode, including `--hook`. A ledger keeps a site from an opt-in check only while that check is enabled, so add the flag to both hook commands and to every check that shares a ledger. From Python, pass `enable=[...]` to `analyze_delta` or `DeltaSession.from_workspace`.

## Track what is still open

A contract change can leave work at other sites. `--ledger` is designed for repeated local, multi-push, and agent-hook deduplication (surviving line edits via enclosing-function anchors without re-alerting); single-shot CI PR gates get identical merge-head output from stateless `--base-ref`. Pass `--ledger` to keep those sites in a JSON file between repeated checks.

```sh
mip-delta --check-git --ledger .mipdelta/ledger.json
mip-delta --check-git --ledger .mipdelta/ledger.json --open
mip-delta --waive 5f39d331 --reason "caller is removed in the next change" --ledger .mipdelta/ledger.json
```

With a ledger, text output names only sites that this check newly opened. A site leaves the ledger when a later edit resolves it or the contract change is reverted. Edits that move a site to another line do not reopen it. `--open` lists every site still open with its id, up to 600 bytes by default, and summarizes the rest as a count per file. `--waive` records that a site is intentional. A waived site is not reported again, and `--fail-on-findings` then fails only while unwaived sites remain. JSON output adds `opened`, `discharged`, `open`, and `waived` lists. The `findings` list still holds every current finding.

A ledger stored in a `.mipdelta` directory adds a `.gitignore` there so the state stays out of Git status.

## Run before a commit

With [pre-commit](https://pre-commit.com), add the root [`.pre-commit-hooks.yaml`](../.pre-commit-hooks.yaml) hook so commits stop while unwaived sites remain open (replace `main` with a commit SHA once pinned):

```yaml
repos:
  - repo: https://github.com/Nitefawkes/mipcode
    rev: main
    hooks:
      - id: mip-delta
```

Or run `mip-delta` from your local environment using `repo: local` (`language: system`) with `entry: mip-delta --check-git --fail-on-findings --ledger .mipdelta/ledger.json`.

The check compares the working tree with `HEAD`, so it also sees changes that are not staged.

## Run on a pull request

In CI, compare the pull request with the point where it left its base branch. This GitHub Actions job prints the report in its log and does not fail the build:

```yaml
jobs:
  contract-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python -m pip install "git+https://github.com/Nitefawkes/mipcode@main"
      - run: mip-delta --check-git --base-ref "$(git merge-base "origin/$BASE" HEAD)"
        env:
          BASE: ${{ github.base_ref }}
```

`fetch-depth: 0` fetches the history that `git merge-base` needs. Add `--fail-on-findings` to make the job fail when the check reports a site. Start without it: on upstream history most reports were not defects, and a CI job has no ledger in which to waive one.

## Filter flagged diffs with Stage-2 micro-triage (`--review`)

To suppress static false alarms on pull requests without giving up cross-file recall, pass `--review`:

```sh
mip-delta --check-git --base-ref "$(git merge-base "origin/$BASE" HEAD)" --review
```

`mip-delta` stays zero-dependency: `--review` invokes an LLM **only** when deterministic Stage 1 emits findings, costing nothing when Stage 1 is silent. It checks `ANTHROPIC_API_KEY` first (calling the Anthropic Messages API over stdlib `urllib`) and falls back to an authenticated `claude` CLI on `PATH` (`--review-model` or `MIPDELTA_REVIEW_MODEL` overrides the default model).

- In text mode, a `suppress_false_alarm` verdict stays silent; `confirm_omission` and `review_worthy` append a `[mip-delta:review <verdict>]` summary line.
- With `--fail-on-findings`, `suppress_false_alarm` exits cleanly with status 0 instead of blocking.
- With `--json`, the payload preserves Stage-1 `findings` and adds a `review` object (`verdict`, `suppressed`, `reason`, `backend`, `model`, `wall_ms`, and optional `cost_usd`).

## Check a merge

Two branches can merge without a textual conflict and still disagree. One changes a function's contract and updates its callers. The other adds a caller that uses the old contract. After merging, compare the merged tree with the merge base:

```sh
mip-delta --check-git --base-ref "$(git merge-base HEAD~1 HEAD^2)"
```

A branch that completed its own migration contributes no findings, so what remains comes from the combination. Use the two branch names in place of `HEAD~1` and `HEAD^2` when checking before the merge commit exists.

## Run as Claude Code hooks

The hooks are experimental. In a [small pilot](../results/pilot/README.md), a current model given three tasks made none of the omissions the hooks would report, so their benefit during an agent's session is unproven.

`--hook` reads a Claude Code hook event on standard input and answers in that hook's JSON format. Register both hooks in `.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [{"type": "command", "command": "mip-delta --hook claude-post-edit", "timeout": 30}]
      }
    ],
    "Stop": [
      {
        "hooks": [{"type": "command", "command": "mip-delta --hook claude-stop", "timeout": 30}]
      }
    ]
  }
}
```

After an edit to a Python file, the first hook adds newly opened sites as context for the model. It prints nothing for other files, for local edits, and for sites it already reported. When the model tries to finish, the second hook blocks once and lists the sites still open. It does not block again for sites it has already listed, so the model can state that a remaining site is intentional and finish.

The hooks use `CLAUDE_PROJECT_DIR` as the workspace and keep one ledger per session under `.mipdelta/` at the top of the repository. The comparison baseline is the commit that was current when the session's first site opened, so a commit made during the session does not hide open sites. A hook reads only the changed files unless one of them changes a contract that other files can depend on. A hook never fails the tool call: a file that does not parse yet, or a directory outside a Git repository, produces no output. Set `MIPDELTA_HOOK_DEBUG=1` to see the reason on stderr.

Each ledger also has an append-only event log beside it: `ledger-<session>.json` writes `ledger-<session>.events.jsonl`. The hooks, `--check-git --ledger`, and `--waive` append one JSON line per opened, discharged, or waived site, per stop block, and per run, with its duration. The log holds names, paths, and line numbers, not source text. A failure to write it is ignored. A session that opens no site leaves no log. To summarize the logs across sessions, see the [dogfood experiment](../experiments/dogfood/README.md).

Other agent harnesses can call `mip-delta --check-git --ledger PATH` after edits and add `--open` before finishing.

## Observe calls during tests (opt-in `--run-pytest`)

By default, `mip-delta` runs pure zero-dependency static AST (~5 ms) and ignores vendored directories (`vendor/`, `_vendor/`, `third_party/`, `<pkg>/packages/`) and `@task` context parameters automatically. When your code dispatches heavily through dynamic attributes (`self.client.request(...)`, plugins, or renderables), `--run-pytest` acts as an **opt-in dynamic supplement** that collects runtime receiver types under Python's PEP 669 `sys.monitoring`.

Use CPython 3.12 or later and install pytest into the same environment. From the MipCode repository root:

```sh
python -m pip install -e ".[test]"
mip-delta --run-pytest --root . --pytest-args="tests -q"
```

The command records up to `max_events_per_site=1` runtime call edge per instruction site in `.mipdelta/runtime/` (`+5.26%` cold / `+14.17%` warm median overhead and `90.0%` mutation recall in [Experiment 2](../results/audit_rt_ledger/README.md)), then checks the Git delta after pytest returns. It preserves pytest's failure status. Use `--runtime-dir PATH` with a fresh directory when collecting evidence for a new source state, since the check can load earlier observations from that directory.

`[static]` marks a relationship resolved from source. `[type-cand]` marks a candidate inferred from types. `[rt-obs]` uses a call observed during tests. Sampling and test coverage limit runtime observations. A quiet check means the configured analysis emitted no advisory.
